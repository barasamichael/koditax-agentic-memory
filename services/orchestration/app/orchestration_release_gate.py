"""Deterministic release-gate and post-deploy verification helpers for orchestration."""

from __future__ import annotations

from typing import Literal
from typing import TypedDict
import hashlib

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.config import OrchestrationReleaseControlConfig
from services.orchestration.app.config import load_orchestration_release_control_config
from services.orchestration.app.orchestration_eval_harness import OrchestrationEvalSummary
from services.orchestration.app.orchestration_eval_harness import run_orchestration_eval_corpus
from services.orchestration.app.orchestration_rollout_manifest import OrchestrationRolloutManifest
from services.orchestration.app.orchestration_rollout_manifest import (
    build_orchestration_rollout_manifest,
)
from services.orchestration.app.orchestration_readiness_summary import OrchestrationReadinessSummary
from services.orchestration.app.orchestration_readiness_summary import (
    build_orchestration_readiness_summary,
)

ReleaseGateStatus = Literal["go", "go_degraded_safe", "no_go"]
VerificationStatus = Literal["verified", "failed"]


class OrchestrationReleaseGateSummary(TypedDict):
    """Represent deterministic orchestration release-gate output."""

    gate_id: str
    gate_status: ReleaseGateStatus
    readiness_status: str
    rollout_status: str
    manifest_id: str
    canary_tenants: list[str]
    general_availability_tenants: list[str]
    blocking_reasons: list[str]
    degraded_reasons: list[str]


class OrchestrationTenantVerification(TypedDict):
    """Represent deterministic tenant verification result for post-deploy checks."""

    tenant_id: str
    expected_status: str
    actual_status: str
    reason_code: str | None


class OrchestrationPostDeployVerificationSummary(TypedDict):
    """Represent deterministic post-deploy verification output."""

    verification_id: str
    status: VerificationStatus
    gate_status: ReleaseGateStatus
    fallback_status: str | None
    tenant_results: list[OrchestrationTenantVerification]
    blocking_reasons: list[str]


def build_orchestration_release_gate(
    *,
    golden_summary: OrchestrationEvalSummary | None = None,
    adversarial_summary: OrchestrationEvalSummary | None = None,
    response_synthesis_enabled: bool = True,
    conversation_continuity_enabled: bool = True,
    release_control_config: OrchestrationReleaseControlConfig | None = None,
    allowlist_document: dict[str, object] | None = None,
) -> OrchestrationReleaseGateSummary:
    """Build deterministic release-gate classification from readiness and rollout state."""

    golden = golden_summary or run_orchestration_eval_corpus(corpus="golden")
    adversarial = adversarial_summary or run_orchestration_eval_corpus(corpus="adversarial")
    readiness = build_orchestration_readiness_summary(
        golden_summary=golden,
        adversarial_summary=adversarial,
        response_synthesis_enabled=response_synthesis_enabled,
        conversation_continuity_enabled=conversation_continuity_enabled,
    )
    manifest = build_orchestration_rollout_manifest(allowlist_document=allowlist_document)
    config = release_control_config or load_orchestration_release_control_config()
    return _build_gate_summary(
        readiness=readiness,
        manifest=manifest,
        release_control_config=config,
    )


def build_orchestration_post_deploy_verification(
    *,
    release_gate: OrchestrationReleaseGateSummary,
    tenant_results: list[OrchestrationTenantVerification],
    fallback_status: str | None,
) -> OrchestrationPostDeployVerificationSummary:
    """Build deterministic post-deploy verification summary from gate and tenant checks."""

    blocking_reasons: list[str] = []
    if release_gate["gate_status"] == "no_go":
        blocking_reasons.append("release_gate_no_go")
    if release_gate["gate_status"] == "go_degraded_safe" and fallback_status != "failed":
        blocking_reasons.append("degraded_safe_fallback_not_observed")
    if release_gate["gate_status"] == "go" and fallback_status is not None:
        blocking_reasons.append("unexpected_fallback_in_full_go_state")
    for result in tenant_results:
        if result["expected_status"] != result["actual_status"]:
            blocking_reasons.append(f"tenant_status_mismatch:{result['tenant_id']}")

    verification_core = {
        "gate_status": release_gate["gate_status"],
        "fallback_status": fallback_status,
        "tenant_results": tenant_results,
        "blocking_reasons": sorted(blocking_reasons),
    }
    verification_id = hashlib.sha256(
        canonical_json_dumps(verification_core).encode("utf-8")
    ).hexdigest()
    return {
        "verification_id": verification_id,
        "status": "verified" if not blocking_reasons else "failed",
        "gate_status": release_gate["gate_status"],
        "fallback_status": fallback_status,
        "tenant_results": tenant_results,
        "blocking_reasons": sorted(blocking_reasons),
    }


def _build_gate_summary(
    *,
    readiness: OrchestrationReadinessSummary,
    manifest: OrchestrationRolloutManifest,
    release_control_config: OrchestrationReleaseControlConfig,
) -> OrchestrationReleaseGateSummary:
    blocking_reasons = list(readiness["blocking_reasons"]) + list(manifest["blocking_reasons"])
    degraded_reasons = list(readiness["degraded_reasons"])
    if release_control_config.require_explicit_canary and not manifest["canary_tenants"]:
        blocking_reasons.append("explicit_canary_required")
    if (
        readiness["status"] == "degraded_safe"
        and not release_control_config.allow_degraded_safe_release
    ):
        blocking_reasons.append("degraded_safe_release_disabled")

    if blocking_reasons:
        gate_status: ReleaseGateStatus = "no_go"
    elif readiness["status"] == "degraded_safe":
        gate_status = "go_degraded_safe"
    else:
        gate_status = "go"

    gate_core = {
        "gate_status": gate_status,
        "readiness_status": readiness["status"],
        "rollout_status": manifest["rollout_status"],
        "manifest_id": manifest["manifest_id"],
        "canary_tenants": manifest["canary_tenants"],
        "general_availability_tenants": manifest["general_availability_tenants"],
        "blocking_reasons": sorted(blocking_reasons),
        "degraded_reasons": sorted(degraded_reasons),
    }
    gate_id = hashlib.sha256(canonical_json_dumps(gate_core).encode("utf-8")).hexdigest()
    return {
        "gate_id": gate_id,
        "gate_status": gate_status,
        "readiness_status": readiness["status"],
        "rollout_status": manifest["rollout_status"],
        "manifest_id": manifest["manifest_id"],
        "canary_tenants": list(manifest["canary_tenants"]),
        "general_availability_tenants": list(manifest["general_availability_tenants"]),
        "blocking_reasons": sorted(blocking_reasons),
        "degraded_reasons": sorted(degraded_reasons),
    }
