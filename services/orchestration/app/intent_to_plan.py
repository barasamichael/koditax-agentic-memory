"""Translate validated intent envelopes into deterministic income-tax orchestration plans."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from typing import TypedDict
from pathlib import Path
from functools import lru_cache
from dataclasses import dataclass

from jsonschema.validators import validator_for

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.trace_context import build_trace_id
from shared.validation.income_tax_capability_manifest import assert_supported_lane
from shared.validation.income_tax_capability_manifest import CapabilityManifestError
from shared.validation.income_tax_capability_manifest import load_income_tax_vertical_slice_manifest
from services.orchestration.app.prompt_intent_envelope import PromptIntentEnvelope
from services.orchestration.app.request_timer import timed_print


class GovernedPlanStep(TypedDict):
    """Represent one canonical governed orchestration plan step."""

    step_id: str
    route_id: str
    target_service: str
    target_operation: str
    step_status: str
    depends_on: list[str]
    step_purpose: str | None


class GovernedOrchestrationPlan(TypedDict):
    """Represent one canonical governed orchestration plan."""

    plan_id: str
    plan_version: str
    plan_status: str
    planning_mode: str
    execution_ready: bool
    steps: list[GovernedPlanStep]


class PlannedRouteSelection(TypedDict):
    """Represent one selected route derived from a governed single-step plan."""

    route_id: str
    target_service: str
    target_operation: str


def _knowledge_template_key(intent_envelope: PromptIntentEnvelope) -> tuple[str, str]:
    intent_class = intent_envelope["intent_class"]
    tax_domain_hint = intent_envelope["tax_domain_hint"]
    if (
        intent_class == "lookup_grounded_knowledge"
        and intent_envelope.get("knowledge_route_mode_hint") == "timeline_search"
    ):
        return ("timeline_grounded_knowledge", tax_domain_hint)
    return (intent_class, tax_domain_hint)


_GOVERNED_STEP_TEMPLATES: dict[tuple[str, str], tuple[dict[str, object], ...]] = {
    ("meta_conversation", "general_tax"): (
        {
            "route_id": "meta_conversation_route_v1",
            "target_service": "orchestration",
            "target_operation": "generate_meta_conversation_response",
            "step_purpose": "conversation_control_response",
        },
    ),
    ("compute_income_tax", "income_tax"): (
        {
            "route_id": "income_tax_compute_route_v1",
            "target_service": "tax_core",
            "target_operation": "execute_computation",
            "step_purpose": "tax_computation",
        },
    ),
    ("compute_health_contribution", "health_contribution"): (
        {
            "route_id": "health_contribution_compute_route_v1",
            "target_service": "tax_core",
            "target_operation": "execute_computation",
            "step_purpose": "tax_computation",
        },
    ),
    ("lookup_grounded_knowledge", "income_tax"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "general_tax"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "health_contribution"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "paye_generalized"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "vat"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "withholding_tax_generalized"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "rental_income_generalized"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("lookup_grounded_knowledge", "business_income_generalized"): (
        {
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
            "step_purpose": "grounded_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "income_tax"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "health_contribution"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "paye_generalized"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "vat"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "withholding_tax_generalized"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "rental_income_generalized"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("timeline_grounded_knowledge", "business_income_generalized"): (
        {
            "route_id": "knowledge_timeline_route_v1",
            "target_service": "knowledge",
            "target_operation": "timeline_search_knowledge",
            "step_purpose": "grounded_temporal_authority_lookup",
        },
    ),
    ("retrieve_grounded_knowledge", "income_tax"): (
        {
            "route_id": "knowledge_retrieve_route_v1",
            "target_service": "knowledge",
            "target_operation": "retrieve_knowledge",
            "step_purpose": "grounded_direct_retrieval",
        },
    ),
    ("retrieve_grounded_knowledge", "health_contribution"): (
        {
            "route_id": "knowledge_retrieve_route_v1",
            "target_service": "knowledge",
            "target_operation": "retrieve_knowledge",
            "step_purpose": "grounded_direct_retrieval",
        },
    ),
    ("generate_form_artifact", "income_tax"): (
        {
            "route_id": "income_tax_form_generation_route_v1",
            "target_service": "forms",
            "target_operation": "generate_income_tax_form_artifact",
            "step_purpose": "form_generation",
        },
    ),
    ("generate_form_artifact", "health_contribution"): (
        {
            "route_id": "health_contribution_form_mapping_route_v1",
            "target_service": "forms",
            "target_operation": "map_health_contribution_output_to_form_ready",
            "step_purpose": "form_generation",
        },
    ),
    ("generate_report_artifact", "income_tax"): (
        {
            "route_id": "income_tax_report_generation_route_v1",
            "target_service": "reports",
            "target_operation": "create_income_tax_report_artifact",
            "step_purpose": "report_generation",
        },
    ),
    ("generate_report_artifact", "health_contribution"): (
        {
            "route_id": "health_contribution_report_generation_route_v1",
            "target_service": "reports",
            "target_operation": "create_health_contribution_report_artifact",
            "step_purpose": "report_generation",
        },
    ),
    ("extract_document", "income_tax"): (
        {
            "route_id": "income_tax_document_evidence_route_v1",
            "target_service": "document_ai",
            "target_operation": "search_document_evidence",
            "step_purpose": "document_evidence",
        },
    ),
}

# No domains are permanently plan-only — all supported domains that have a
# knowledge search route can execute. Removing paye_generalized from this set
# allows PAYE knowledge queries to execute rather than returning plan_only.
_PLAN_ONLY_DOMAINS: frozenset[str] = frozenset()


class RejectedIntentContext(TypedDict):
    """Represent deterministic rejected context for intent-to-plan failures."""

    tax_domain_hint: str
    requested_lane_hint: str | None
    historical_version_hint: str | None
    tax_year_hint: int | None
    intent_class: str
    prompt_class: str


class IncomeTaxOrchestrationStep(TypedDict):
    """Represent one deterministic step in supported income-tax orchestration plan."""

    step_order: int
    step_id: str
    step_type: str
    module_ref: str
    action_ref: str
    external_action: bool


class IncomeTaxOrchestrationPlan(TypedDict):
    """Represent deterministic orchestration plan for one supported intent envelope."""

    plan_id: str
    intent_class: str
    supported_lane_id: str
    historical_version_id: str
    tax_year: int
    steps: list[IncomeTaxOrchestrationStep]
    plan_status: str
    correlation_id: str
    trace_id: str


@dataclass(frozen=True)
class _PlanStepDefinition:
    step_id: str
    step_type: str
    module_ref: str
    action_ref: str


PLAN_STEP_DEFINITIONS: tuple[_PlanStepDefinition, ...] = (
    _PlanStepDefinition(
        step_id="capability_check",
        step_type="guardrail",
        module_ref="services.orchestration.app.income_tax_capability_gate",
        action_ref="enforce_income_tax_runtime_capability_gate",
    ),
    _PlanStepDefinition(
        step_id="income_tax_computation",
        step_type="computation",
        module_ref="services.tax_core.app.engine.executor",
        action_ref="execute_computation",
    ),
    _PlanStepDefinition(
        step_id="form_mapping",
        step_type="forms",
        module_ref="services.forms.app.income_tax.form_mapping",
        action_ref="map_finalized_income_tax_output_to_form_ready",
    ),
    _PlanStepDefinition(
        step_id="form_version_binding",
        step_type="forms",
        module_ref="services.forms.app.income_tax.form_version_binding",
        action_ref="bind_income_tax_form_version",
    ),
    _PlanStepDefinition(
        step_id="form_artifact_generation",
        step_type="forms",
        module_ref="services.forms.app.income_tax.form_artifact_generation",
        action_ref="generate_income_tax_form_artifact",
    ),
    _PlanStepDefinition(
        step_id="report_generation",
        step_type="reporting",
        module_ref="services.forms.app.income_tax.report_generation",
        action_ref="generate_income_tax_report",
    ),
    _PlanStepDefinition(
        step_id="report_version_binding",
        step_type="reporting",
        module_ref="services.forms.app.income_tax.report_version_binding",
        action_ref="bind_income_tax_report_version",
    ),
    _PlanStepDefinition(
        step_id="submission_payload_construction",
        step_type="submission",
        module_ref="services.forms.app.income_tax.submission_payload_construction",
        action_ref="construct_income_tax_submission_payload",
    ),
    _PlanStepDefinition(
        step_id="submission_workflow_initialize",
        step_type="submission",
        module_ref="services.forms.app.income_tax.submission_workflow",
        action_ref="initialize_income_tax_submission_workflow",
    ),
    _PlanStepDefinition(
        step_id="submission_workflow_ready_transition",
        step_type="submission",
        module_ref="services.forms.app.income_tax.submission_workflow",
        action_ref="advance_income_tax_submission_workflow(ready_for_submission)",
    ),
    _PlanStepDefinition(
        step_id="submission_workflow_internal_submit_transition",
        step_type="submission",
        module_ref="services.forms.app.income_tax.submission_workflow",
        action_ref="advance_income_tax_submission_workflow(submitted_internal)",
    ),
    _PlanStepDefinition(
        step_id="submission_closure",
        step_type="submission",
        module_ref="services.forms.app.income_tax.submission_audit_closure",
        action_ref="close_income_tax_submission_workflow",
    ),
)


class IntentToPlanError(RuntimeError):
    """Represent deterministic intent-to-plan translation failures."""

    def __init__(
        self,
        *,
        error_code: str,
        reason: str,
        message: str,
        rejected_context: RejectedIntentContext,
        correlation_id: str,
        trace_id: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.reason = reason
        self.message = message
        self.rejected_context = rejected_context
        self.correlation_id = correlation_id
        self.trace_id = trace_id

    def payload(self) -> dict[str, object]:
        """Return canonical deterministic rejection payload for intent-to-plan boundary."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "rejected_context": self.rejected_context,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
        }


def translate_income_tax_intent_to_plan(
    intent_envelope: PromptIntentEnvelope,
) -> IncomeTaxOrchestrationPlan:
    """Translate one validated prompt intent envelope into deterministic orchestration plan."""

    correlation_id = intent_envelope["correlation_id"]
    trace_id = intent_envelope.get("trace_id", build_trace_id(correlation_id))
    rejected_context: RejectedIntentContext = {
        "tax_domain_hint": intent_envelope["tax_domain_hint"],
        "requested_lane_hint": intent_envelope["requested_lane_hint"],
        "historical_version_hint": intent_envelope["historical_version_hint"],
        "tax_year_hint": intent_envelope["tax_year_hint"],
        "intent_class": intent_envelope["intent_class"],
        "prompt_class": intent_envelope["prompt_class"],
    }

    if intent_envelope["prompt_class"] != "income_tax_prompt_flow":
        _emit_plan_generated_event(
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": "unsupported_prompt_class"},
            supported_lane_id=intent_envelope["requested_lane_hint"],
            historical_version_id=intent_envelope["historical_version_hint"],
            tax_year=intent_envelope["tax_year_hint"],
        )
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason="unsupported_prompt_class",
            message="Prompt class is not supported for income-tax intent-to-plan translation.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    if intent_envelope["tax_domain_hint"] != "income_tax":
        _emit_plan_generated_event(
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": "unsupported_domain"},
            supported_lane_id=intent_envelope["requested_lane_hint"],
            historical_version_id=intent_envelope["historical_version_hint"],
            tax_year=intent_envelope["tax_year_hint"],
        )
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason="unsupported_domain",
            message="Intent envelope domain is outside governed income-tax pilot scope.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    if intent_envelope["requested_lane_hint"] is None and (
        intent_envelope["historical_version_hint"] is not None
        or intent_envelope["tax_year_hint"] is not None
    ):
        _emit_plan_generated_event(
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": "unsupported_lane_context"},
            supported_lane_id=intent_envelope["requested_lane_hint"],
            historical_version_id=intent_envelope["historical_version_hint"],
            tax_year=intent_envelope["tax_year_hint"],
        )
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason="unsupported_lane_context",
            message=(
                "Intent envelope lane/version context is not supported by governed pilot scope."
            ),
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    if intent_envelope["intent_class"] != "compute_income_tax":
        _emit_plan_generated_event(
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": "unsupported_intent_class"},
            supported_lane_id=intent_envelope["requested_lane_hint"],
            historical_version_id=intent_envelope["historical_version_hint"],
            tax_year=intent_envelope["tax_year_hint"],
        )
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason="unsupported_intent_class",
            message="Intent class is not supported for deterministic orchestration planning.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    supported_lane_id = intent_envelope["requested_lane_hint"]
    historical_version_id = intent_envelope["historical_version_hint"]
    tax_year = intent_envelope["tax_year_hint"]
    if supported_lane_id is None or historical_version_id is None or tax_year is None:
        _emit_plan_generated_event(
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": "missing_lane_context"},
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason="missing_lane_context",
            message=(
                "Intent envelope does not contain lane/version/year context required for "
                "deterministic plan generation."
            ),
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )

    try:
        timed_print("[PLAN] About to load income-tax capability manifest")
        manifest = load_income_tax_vertical_slice_manifest()
        timed_print("[PLAN] Loaded income-tax capability manifest")
        timed_print("[PLAN] About to validate supported lane against manifest")
        assert_supported_lane(
            manifest,
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )
        timed_print("[PLAN] Validated supported lane against manifest")
    except CapabilityManifestError as error:
        _emit_plan_generated_event(
            status="rejected",
            correlation_id=correlation_id,
            trace_id=trace_id,
            context={"reason": error.reason},
            supported_lane_id=supported_lane_id,
            historical_version_id=historical_version_id,
            tax_year=tax_year,
        )
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason=error.reason,
            message="Intent envelope lane/version context is outside manifest-supported scope.",
            rejected_context=rejected_context,
            correlation_id=correlation_id,
            trace_id=trace_id,
        ) from error

    timed_print("[PLAN] About to build governed plan steps")
    steps = _build_plan_steps()
    timed_print(
        "[PLAN] Built governed plan steps "
        f"step_count={len(steps)}"
    )
    plan_id = _build_plan_id(
        intent_class=intent_envelope["intent_class"],
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        correlation_id=correlation_id,
        trace_id=trace_id,
        steps=steps,
    )
    plan: IncomeTaxOrchestrationPlan = {
        "plan_id": plan_id,
        "intent_class": intent_envelope["intent_class"],
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "steps": steps,
        "plan_status": "planned",
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    timed_print("[PLAN] About to validate income-tax orchestration plan")
    validate_income_tax_orchestration_plan(plan)
    timed_print("[PLAN] Validated income-tax orchestration plan")
    _emit_plan_generated_event(
        status="generated",
        correlation_id=correlation_id,
        trace_id=trace_id,
        context={"plan_id": plan_id, "step_count": len(steps)},
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )
    return plan


def validate_income_tax_orchestration_plan(plan: IncomeTaxOrchestrationPlan) -> None:
    """Validate one orchestration plan against canonical JSON schema."""

    schema = _load_income_tax_orchestration_plan_schema()
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator_class(schema).validate(cast(Any, plan))


def _build_plan_steps() -> list[IncomeTaxOrchestrationStep]:
    steps: list[IncomeTaxOrchestrationStep] = []
    for index, step in enumerate(PLAN_STEP_DEFINITIONS, start=1):
        steps.append(
            {
                "step_order": index,
                "step_id": step.step_id,
                "step_type": step.step_type,
                "module_ref": step.module_ref,
                "action_ref": step.action_ref,
                "external_action": False,
            }
        )
    return steps


def _build_plan_id(
    *,
    intent_class: str,
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
    correlation_id: str,
    trace_id: str,
    steps: list[IncomeTaxOrchestrationStep],
) -> str:
    digest_input = {
        "intent_class": intent_class,
        "supported_lane_id": supported_lane_id,
        "historical_version_id": historical_version_id,
        "tax_year": tax_year,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "step_ids": [step["step_id"] for step in steps],
    }
    return _sha256_hex(canonical_json_dumps(digest_input))


def _sha256_hex(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _emit_plan_generated_event(
    *,
    status: str,
    correlation_id: str,
    trace_id: str,
    context: dict[str, object],
    supported_lane_id: str | None,
    historical_version_id: str | None,
    tax_year: int | None,
) -> None:
    emit_income_tax_audit_event(
        event_type="plan_generated",
        status=status,
        correlation_id=correlation_id,
        trace_id=trace_id,
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
        context=context,
    )


@lru_cache(maxsize=1)
def _load_income_tax_orchestration_plan_schema() -> dict[str, object]:
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "tools"
        / "schemas"
        / "income_tax_orchestration_plan.schema.json"
    )
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))


def build_governed_orchestration_plan(
    intent_envelope: PromptIntentEnvelope,
) -> GovernedOrchestrationPlan:
    """Build one canonical governed orchestration plan for prompt decisioning."""

    intent_class = intent_envelope["intent_class"]
    tax_domain_hint = intent_envelope["tax_domain_hint"]
    planning_mode_hint = intent_envelope.get("planning_mode_hint", "single_step")

    if intent_class == "clarification_required":
        return {
            "plan_id": _build_governed_plan_id(
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                planning_mode="clarification_required",
                execution_ready=False,
                step_fingerprints=(),
            ),
            "plan_version": "2.0.0",
            "plan_status": "clarification_required",
            "planning_mode": "clarification_required",
            "execution_ready": False,
            "steps": [],
        }

    if intent_class == "compute_plus_grounding" and tax_domain_hint in {
        "income_tax",
        "health_contribution",
    }:
        compute_templates = _GOVERNED_STEP_TEMPLATES[
            (
                "compute_income_tax"
                if tax_domain_hint == "income_tax"
                else "compute_health_contribution",
                tax_domain_hint,
            )
        ]
        knowledge_templates = _GOVERNED_STEP_TEMPLATES[
            (
                "timeline_grounded_knowledge"
                if intent_envelope.get("knowledge_route_mode_hint") == "timeline_search"
                else "lookup_grounded_knowledge",
                tax_domain_hint,
            )
        ]
        steps = _build_governed_steps(compute_templates + knowledge_templates)
        steps[1]["depends_on"] = [steps[0]["step_id"]]
        needs_disambiguation = _envelope_needs_disambiguation(intent_envelope)
        if needs_disambiguation:
            steps = _prepend_disambiguation_step(steps)
        return _canonicalize_governed_plan({
            "plan_id": _build_governed_plan_id(
                intent_class=intent_class,
                tax_domain_hint=tax_domain_hint,
                planning_mode="multi_step",
                execution_ready=False,
                step_fingerprints=tuple(step["step_id"] for step in steps),
            ),
            "plan_version": "2.0.0",
            "plan_status": "planned" if not needs_disambiguation else "awaiting_disambiguation",
            "planning_mode": "multi_step",
            "execution_ready": False,
            "steps": steps,
        })

    templates = _GOVERNED_STEP_TEMPLATES.get(_knowledge_template_key(intent_envelope))
    if templates is None:
        # Graceful degradation: attempt a domain-aware fallback plan rather than raising.
        degraded = _build_degraded_plan(intent_envelope)
        if degraded is not None:
            return degraded
        raise IntentToPlanError(
            error_code="unsupported_intent_plan",
            reason="unsupported_intent_class",
            message="Intent class is not supported for governed orchestration planning.",
            rejected_context={
                "tax_domain_hint": tax_domain_hint,
                "requested_lane_hint": intent_envelope["requested_lane_hint"],
                "historical_version_hint": intent_envelope["historical_version_hint"],
                "tax_year_hint": intent_envelope["tax_year_hint"],
                "intent_class": intent_class,
                "prompt_class": intent_envelope["prompt_class"],
            },
            correlation_id=intent_envelope["correlation_id"],
            trace_id=intent_envelope["trace_id"],
        )
    steps = _build_governed_steps(templates)
    execution_ready = (
        planning_mode_hint == "single_step" and tax_domain_hint not in _PLAN_ONLY_DOMAINS
    )
    return _canonicalize_governed_plan({
        "plan_id": _build_governed_plan_id(
            intent_class=intent_class,
            tax_domain_hint=tax_domain_hint,
            planning_mode=planning_mode_hint,
            execution_ready=execution_ready,
            step_fingerprints=tuple(step["step_id"] for step in steps),
        ),
        "plan_version": "2.0.0",
        "plan_status": "planned",
        "planning_mode": planning_mode_hint,
        "execution_ready": execution_ready,
        "steps": steps,
    })


def extract_selected_route_from_governed_plan(
    plan: GovernedOrchestrationPlan,
) -> PlannedRouteSelection | None:
    """Return one selected route when a governed plan is single-step and executable."""

    if plan["planning_mode"] != "single_step":
        return None
    if len(plan["steps"]) != 1:
        return None
    step = plan["steps"][0]
    return {
        "route_id": step["route_id"],
        "target_service": step["target_service"],
        "target_operation": step["target_operation"],
    }


def _build_governed_steps(
    templates: tuple[dict[str, object], ...],
) -> list[GovernedPlanStep]:
    steps: list[GovernedPlanStep] = []
    for template in templates:
        route_id = str(template["route_id"])
        target_service = str(template["target_service"])
        target_operation = str(template["target_operation"])
        step_purpose = cast(str | None, template.get("step_purpose"))
        step_id = _sha256_hex(
            canonical_json_dumps(
                {
                    "scope": "governed_orchestration_plan_step",
                    "route_id": route_id,
                    "target_service": target_service,
                    "target_operation": target_operation,
                    "step_purpose": step_purpose,
                }
            )
        )
        steps.append(
            {
                "step_id": step_id,
                "route_id": route_id,
                "target_service": target_service,
                "target_operation": target_operation,
                "step_status": "planned",
                "depends_on": [],
                "step_purpose": step_purpose,
            }
        )
    return steps


def _envelope_needs_disambiguation(envelope: PromptIntentEnvelope) -> bool:
    """Return True when the envelope lacks enough context to execute without clarification."""
    if envelope.get("requested_lane_hint") is None:
        return True
    if envelope.get("tax_year_hint") is None:
        return True
    confidence = envelope.get("semantic_extraction_confidence")
    if isinstance(confidence, float) and confidence < 0.60:
        return True
    return False


def _prepend_disambiguation_step(
    steps: list[GovernedPlanStep],
) -> list[GovernedPlanStep]:
    """Insert a no-op disambiguation marker step before the compute step.

    The step carries step_purpose='disambiguation_required' so downstream
    executors and the synthesis layer can detect it and surface a clarification
    prompt rather than attempting blind execution.
    """
    disambiguation_step_id = _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "governed_orchestration_plan_step",
                "route_id": "disambiguation_route_v1",
                "target_service": "orchestration",
                "target_operation": "request_disambiguation",
                "step_purpose": "disambiguation_required",
            }
        )
    )
    disambiguation_step: GovernedPlanStep = {
        "step_id": disambiguation_step_id,
        "route_id": "disambiguation_route_v1",
        "target_service": "orchestration",
        "target_operation": "request_disambiguation",
        "step_status": "planned",
        "depends_on": [],
        "step_purpose": "disambiguation_required",
    }
    # Make the first real step depend on disambiguation resolving.
    enriched: list[GovernedPlanStep] = [disambiguation_step]
    for index, step in enumerate(steps):
        updated = dict(step)
        if index == 0:
            updated["depends_on"] = [disambiguation_step_id]
        enriched.append(cast(GovernedPlanStep, updated))
    return enriched


# Compute-like intent classes that can safely degrade to a knowledge search.
_COMPUTE_INTENT_CLASSES = frozenset(
    {"compute_income_tax", "compute_health_contribution", "compute_plus_grounding"}
)

# Knowledge-like intent classes that can safely degrade to a search-only plan.
_KNOWLEDGE_INTENT_CLASSES = frozenset({"lookup_grounded_knowledge", "retrieve_grounded_knowledge"})


def _build_degraded_plan(
    intent_envelope: PromptIntentEnvelope,
) -> GovernedOrchestrationPlan | None:
    """Return a graceful degraded plan when the exact template lookup failed.

    Degradation rules (in priority order):
    1. Compute intent on unsupported domain → clarification_required plan.
    2. Knowledge intent on unsupported domain → search fallback on paye_generalized.
    3. Unknown intent class on any domain → clarification_required plan.
    """
    intent_class = intent_envelope["intent_class"]
    tax_domain_hint = intent_envelope["tax_domain_hint"]

    if intent_class in _COMPUTE_INTENT_CLASSES:
        # Unsupported domain for computation — ask for clarification.
        return {
            "plan_id": _build_governed_plan_id(
                intent_class="clarification_required",
                tax_domain_hint=tax_domain_hint,
                planning_mode="clarification_required",
                execution_ready=False,
                step_fingerprints=(),
            ),
            "plan_version": "2.0.0",
            "plan_status": "clarification_required",
            "planning_mode": "clarification_required",
            "execution_ready": False,
            "steps": [],
        }

    if intent_class in _KNOWLEDGE_INTENT_CLASSES:
        # Unsupported domain for knowledge — fall back to the generalised search lane.
        fallback_templates = _GOVERNED_STEP_TEMPLATES.get(
            ("lookup_grounded_knowledge", "paye_generalized")
        )
        if fallback_templates is not None:
            steps = _build_governed_steps(fallback_templates)
            return {
                "plan_id": _build_governed_plan_id(
                    intent_class=intent_class,
                    tax_domain_hint="paye_generalized",
                    planning_mode="single_step",
                    execution_ready=True,
                    step_fingerprints=tuple(step["step_id"] for step in steps),
                ),
                "plan_version": "2.0.0",
                "plan_status": "planned",
                "planning_mode": "single_step",
                "execution_ready": True,
                "steps": steps,
            }

    # Unknown intent class — surface clarification.
    return {
        "plan_id": _build_governed_plan_id(
            intent_class="clarification_required",
            tax_domain_hint=tax_domain_hint,
            planning_mode="clarification_required",
            execution_ready=False,
            step_fingerprints=(),
        ),
        "plan_version": "2.0.0",
        "plan_status": "clarification_required",
        "planning_mode": "clarification_required",
        "execution_ready": False,
        "steps": [],
    }


def _canonicalize_governed_plan(plan: GovernedOrchestrationPlan) -> GovernedOrchestrationPlan:
    """Canonicalize planning_mode from the final step shape."""

    step_count = len(plan["steps"])
    if step_count == 0:
        return plan
    canonical_mode = "single_step" if step_count == 1 else "multi_step"
    updated = dict(plan)
    updated["planning_mode"] = canonical_mode
    if step_count == 1:
        updated["execution_ready"] = True
    return cast(GovernedOrchestrationPlan, updated)


def _build_governed_plan_id(
    *,
    intent_class: str,
    tax_domain_hint: str,
    planning_mode: str,
    execution_ready: bool,
    step_fingerprints: tuple[str, ...],
) -> str:
    return _sha256_hex(
        canonical_json_dumps(
            {
                "scope": "governed_orchestration_plan",
                "intent_class": intent_class,
                "tax_domain_hint": tax_domain_hint,
                "planning_mode": planning_mode,
                "execution_ready": execution_ready,
                "step_fingerprints": step_fingerprints,
            }
        )
    )
