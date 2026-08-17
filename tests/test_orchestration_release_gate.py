"""Release-gate classification checks for governed orchestration rollout."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import cast

from services.orchestration.app.config import OrchestrationReleaseControlConfig
from services.orchestration.app.orchestration_eval_harness import run_orchestration_eval_corpus
from services.orchestration.app.orchestration_release_gate import build_orchestration_release_gate


def test_release_gate_reports_no_go_when_readiness_is_not_ready() -> None:
    gate = build_orchestration_release_gate()

    assert gate["gate_status"] == "no_go"
    assert gate["readiness_status"] == "not_ready"
    assert gate["blocking_reasons"]
    assert "pilot_tenant_limited" in gate["canary_tenants"]
    assert "pilot_tenant_alpha" in gate["general_availability_tenants"]


def test_release_gate_reports_no_go_when_degraded_rollout_remains_blocked() -> None:
    gate = build_orchestration_release_gate(
        response_synthesis_enabled=False,
        release_control_config=OrchestrationReleaseControlConfig(
            allow_degraded_safe_release=True,
            require_explicit_canary=True,
        ),
    )

    assert gate["gate_status"] == "no_go"
    assert gate["readiness_status"] == "not_ready"
    assert gate["blocking_reasons"]
    assert gate["degraded_reasons"] == []


def test_release_gate_reports_no_go_when_rollout_state_is_missing() -> None:
    allowlist = cast(
        dict[str, object],
        {
            "allowlist_version": "1.0.0",
            "capability_scope": "income_tax_vertical_slice",
            "generated_at": "2026-04-22T00:00:00Z",
            "tenants": [
                {
                    "tenant_id": "pilot_tenant_alpha",
                    "status": "enabled",
                    "allowed_lanes": ["*"],
                    "allowed_actions": ["*"],
                }
            ],
        },
    )

    gate = build_orchestration_release_gate(allowlist_document=allowlist)

    assert gate["gate_status"] == "no_go"
    assert "missing_rollout_state:pilot_tenant_alpha" in gate["blocking_reasons"]


def test_release_gate_reports_no_go_when_required_release_evidence_is_missing() -> None:
    golden = run_orchestration_eval_corpus(corpus="golden")
    adversarial = run_orchestration_eval_corpus(corpus="adversarial")
    broken = deepcopy(golden)
    broken["case_ids"] = broken["case_ids"][:-1]

    gate = build_orchestration_release_gate(
        golden_summary=broken,
        adversarial_summary=adversarial,
    )

    assert gate["gate_status"] == "no_go"
    assert "golden_eval_failures" in gate["blocking_reasons"]


def test_release_gate_is_byte_equivalent_across_repeated_runs() -> None:
    first = build_orchestration_release_gate()
    second = build_orchestration_release_gate()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
