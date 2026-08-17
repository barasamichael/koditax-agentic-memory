"""Enforce manifest-driven runtime capability gating for income-tax prompt flows."""

from __future__ import annotations

from typing import cast
from typing import TypedDict
from typing import NotRequired

from services.orchestration.app.kill_switch_guard import (
    evaluate_income_tax_capability_safety_controls,
)
from shared.validation.income_tax_capability_manifest import assert_supported_lane
from shared.validation.income_tax_capability_manifest import CapabilityManifestError
from shared.validation.income_tax_capability_manifest import load_income_tax_vertical_slice_manifest
from services.orchestration.app.pilot_tenant_guardrails import (
    evaluate_income_tax_pilot_tenant_for_capability,
)


class RejectedPromptContext(TypedDict):
    """Represent deterministic rejected scope context for runtime gate failures."""

    supported_lane_id: str | None
    historical_version_id: str | None
    tax_year: int | None
    tax_domain: str
    prompt_class: str
    tenant_id: NotRequired[str | None]


class CapabilityGateDecision(TypedDict):
    """Represent deterministic runtime gate allow decision."""

    gate_status: str
    capability_scope: str
    manifest_version: str
    supported_lane_id: str
    historical_version_id: str
    tax_year: int


class IncomeTaxCapabilityGateError(RuntimeError):
    """Represent deterministic capability-gate rejection payload."""

    def __init__(
        self,
        *,
        error_code: str,
        reason: str,
        message: str,
        rejected_context: RejectedPromptContext,
        correlation_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.reason = reason
        self.message = message
        self.rejected_context = rejected_context
        self.correlation_id = correlation_id
        self.reason_code = reason_code

    def payload(self) -> dict[str, object]:
        """Return stable structured gate error payload."""

        payload: dict[str, object] = {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "rejected_context": self.rejected_context,
        }
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        if self.reason_code is not None:
            payload["reason_code"] = self.reason_code
        return payload


def enforce_income_tax_runtime_capability_gate(
    *,
    prompt_text: str,
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
    correlation_id: str | None = None,
    tenant_id: str | None = "pilot_tenant_alpha",
) -> CapabilityGateDecision:
    """Allow only governed manifest-supported income-tax lane contexts."""

    print(f"[GATE] Evaluating prompt: {prompt_text!r}")
    print(f"[GATE] Context: lane={supported_lane_id!r}  version={historical_version_id!r}  year={tax_year!r}  tenant={tenant_id!r}")

    rejected_context: RejectedPromptContext = {
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "tax_domain": "income_tax",
        "prompt_class": "income_tax_prompt_flow",
    }

    try:
        manifest = load_income_tax_vertical_slice_manifest()
        print(f"[GATE] Manifest loaded: version={manifest.get('manifest_version')!r}")
    except CapabilityManifestError as error:
        print(f"[GATE] BLOCK — manifest_load_failure: {error.message}")
        raise IncomeTaxCapabilityGateError(
            error_code="unsupported_prompt_scope",
            reason="manifest_load_failure",
            message="Capability manifest could not be loaded for runtime gate enforcement.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
        ) from error

    manifest_version = cast(str, manifest["manifest_version"])
    capability_scope = cast(str, manifest["capability_scope"])
    print(f"[GATE] Domain check skipped: domain already verified as income_tax by router before gate")

    if supported_lane_id is None or historical_version_id is None or tax_year is None:
        missing = [
            name
            for name, val in (
                ("supported_lane_id", supported_lane_id),
                ("historical_version_id", historical_version_id),
                ("tax_year", tax_year),
            )
            if val is None
        ]
        print(f"[GATE] BLOCK — missing_lane_context: missing fields={missing}")
        raise IncomeTaxCapabilityGateError(
            error_code="unsupported_prompt_scope",
            reason="missing_lane_context",
            message=(
                "Prompt context does not contain governed lane/version identity for runtime gate."
            ),
            rejected_context=rejected_context,
            correlation_id=correlation_id,
        )

    print(f"[GATE] Lane context present: all required fields supplied")

    tenant_guard = evaluate_income_tax_pilot_tenant_for_capability(
        tenant_id=tenant_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
    )
    if tenant_guard["guard_status"] != "allowed":
        print(
            f"[GATE] BLOCK — pilot_tenant_not_allowed: tenant={tenant_guard['tenant_id']!r} "
            f"reason_code={tenant_guard['reason_code']!r} reason={tenant_guard['reason']!r}"
        )
        tenant_rejected_context: RejectedPromptContext = {
            **rejected_context,
            "tenant_id": tenant_guard["tenant_id"],
        }
        raise IncomeTaxCapabilityGateError(
            error_code="pilot_tenant_not_allowed",
            reason=tenant_guard["reason_code"],
            message=tenant_guard["reason"],
            rejected_context=tenant_rejected_context,
            correlation_id=correlation_id,
            reason_code=tenant_guard["reason_code"],
        )

    print(f"[GATE] Tenant check passed: tenant={tenant_guard['tenant_id']!r}")

    try:
        assert_supported_lane(
            manifest,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )
        print(f"[GATE] Manifest lane check passed: ({supported_lane_id!r}, {historical_version_id!r}, {tax_year!r})")
    except CapabilityManifestError as error:
        print(
            f"[GATE] BLOCK — unsupported_lane_context: "
            f"({supported_lane_id!r}, {historical_version_id!r}, {tax_year!r}) "
            f"not in manifest supported_lanes  reason={error.reason!r}"
        )
        raise IncomeTaxCapabilityGateError(
            error_code="unsupported_prompt_scope",
            reason=error.reason,
            message="Prompt scope is not supported by governed income-tax pilot capability.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
        ) from error

    safety_decision = evaluate_income_tax_capability_safety_controls(
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
    )
    if safety_decision["control_status"] != "allowed":
        print(
            f"[GATE] BLOCK — safety_controls: lane={supported_lane_id!r} "
            f"reason_code={safety_decision['reason_code']!r} reason={safety_decision['reason']!r}"
        )
        raise IncomeTaxCapabilityGateError(
            error_code="unsupported_prompt_scope",
            reason=safety_decision["reason_code"],
            message="Prompt scope is blocked by deterministic pilot safety controls.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            reason_code=safety_decision["reason_code"],
        )

    print(
        f"[GATE] ALLOW — all checks passed: lane={supported_lane_id!r} "
        f"version={historical_version_id!r} year={tax_year!r}"
    )
    return {
        "gate_status": "allowed",
        "capability_scope": capability_scope,
        "manifest_version": manifest_version,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
    }
