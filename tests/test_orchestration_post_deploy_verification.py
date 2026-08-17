"""Post-deploy verification checks for governed orchestration rollout."""

from __future__ import annotations

import json
from typing import cast

from services.orchestration.app.orchestration_release_gate import OrchestrationTenantVerification
from services.orchestration.app.orchestration_release_gate import build_orchestration_release_gate
from services.orchestration.app.orchestration_release_gate import (
    build_orchestration_post_deploy_verification,
)


def test_post_deploy_verification_fails_when_release_gate_is_not_go() -> None:
    gate = build_orchestration_release_gate()

    verification = build_orchestration_post_deploy_verification(
        release_gate=gate,
        tenant_results=[
            {
                "tenant_id": "pilot_tenant_limited",
                "expected_status": "allowed",
                "actual_status": "allowed",
                "reason_code": "pilot_tenant_allow",
            },
            {
                "tenant_id": "pilot_tenant_disabled",
                "expected_status": "blocked",
                "actual_status": "blocked",
                "reason_code": "tenant_disabled",
            },
        ],
        fallback_status=None,
    )

    assert verification["status"] == "failed"
    assert verification["gate_status"] == "no_go"
    assert "release_gate_no_go" in verification["blocking_reasons"]


def test_post_deploy_verification_fails_when_degraded_gate_does_not_clear() -> None:
    gate = build_orchestration_release_gate(response_synthesis_enabled=False)

    verification = build_orchestration_post_deploy_verification(
        release_gate=gate,
        tenant_results=[
            {
                "tenant_id": "pilot_tenant_limited",
                "expected_status": "allowed",
                "actual_status": "allowed",
                "reason_code": "pilot_tenant_allow",
            }
        ],
        fallback_status="failed",
    )

    assert verification["status"] == "failed"
    assert verification["gate_status"] == "no_go"
    assert "release_gate_no_go" in verification["blocking_reasons"]


def test_post_deploy_verification_fails_when_release_gate_is_no_go() -> None:
    gate = build_orchestration_release_gate(
        allowlist_document={
            "allowlist_version": "1.0.0",
            "capability_scope": "income_tax_vertical_slice",
            "generated_at": "2026-04-22T00:00:00Z",
            "tenants": [],
        }
    )

    verification = build_orchestration_post_deploy_verification(
        release_gate=gate,
        tenant_results=[],
        fallback_status=None,
    )

    assert verification["status"] == "failed"
    assert "release_gate_no_go" in verification["blocking_reasons"]


def test_post_deploy_verification_fails_when_tenant_result_mismatches_expected_status() -> None:
    gate = build_orchestration_release_gate()

    verification = build_orchestration_post_deploy_verification(
        release_gate=gate,
        tenant_results=[
            {
                "tenant_id": "pilot_tenant_disabled",
                "expected_status": "blocked",
                "actual_status": "allowed",
                "reason_code": "pilot_tenant_allow",
            }
        ],
        fallback_status=None,
    )

    assert verification["status"] == "failed"
    assert "tenant_status_mismatch:pilot_tenant_disabled" in verification["blocking_reasons"]


def test_post_deploy_verification_is_byte_equivalent_across_repeated_runs() -> None:
    gate = build_orchestration_release_gate()
    tenant_results = cast(
        list[OrchestrationTenantVerification],
        [
            {
                "tenant_id": "pilot_tenant_limited",
                "expected_status": "allowed",
                "actual_status": "allowed",
                "reason_code": "pilot_tenant_allow",
            }
        ],
    )

    first = build_orchestration_post_deploy_verification(
        release_gate=gate,
        tenant_results=tenant_results,
        fallback_status=None,
    )
    second = build_orchestration_post_deploy_verification(
        release_gate=gate,
        tenant_results=tenant_results,
        fallback_status=None,
    )

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
