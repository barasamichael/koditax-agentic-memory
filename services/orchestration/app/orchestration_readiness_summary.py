"""Deterministic readiness classification for governed orchestration eval coverage."""

from __future__ import annotations

from typing import TypedDict

from services.orchestration.app.orchestration_eval_harness import OrchestrationEvalSummary


class OrchestrationCoverageSummary(TypedDict):
    """Represent deterministic coverage booleans for orchestration readiness."""

    execution_replay_safety: bool
    audit_persistence_coverage: bool
    synthesis_fallback_coverage: bool
    continuity_boundary_coverage: bool
    adversarial_fail_closed_coverage: bool
    supported_service_family_coverage: bool


class OrchestrationReadinessSummary(TypedDict):
    """Represent deterministic orchestration release-readiness classification."""

    status: str
    golden_cases_passed: bool
    adversarial_cases_passed: bool
    coverage: OrchestrationCoverageSummary
    required_case_ids_present: bool
    blocking_reasons: list[str]
    degraded_reasons: list[str]


_REQUIRED_GOLDEN_CASE_IDS = frozenset(
    {
        "golden_compute_single_step",
        "golden_grounded_knowledge_single_step",
        "golden_compute_plus_grounding_multi_step",
        "golden_forms_execution",
        "golden_reports_execution",
        "golden_document_extraction_execution",
        "golden_same_conversation_followup_reuse",
        "golden_synthesis_disabled_fallback",
    }
)
_REQUIRED_ADVERSARIAL_CASE_IDS = frozenset(
    {
        "adversarial_route_override_attempt",
        "adversarial_unsupported_service_forcing",
        "adversarial_cross_conversation_leakage",
        "adversarial_cross_user_followup_leakage",
        "adversarial_citation_invention",
        "adversarial_blocked_continuity_rollout",
        "adversarial_blocked_synthesis_rollout",
        "adversarial_blocked_multi_step_execution",
        "adversarial_malformed_forms_payload",
    }
)


def build_orchestration_readiness_summary(
    *,
    golden_summary: OrchestrationEvalSummary,
    adversarial_summary: OrchestrationEvalSummary,
    response_synthesis_enabled: bool = True,
    conversation_continuity_enabled: bool = True,
) -> OrchestrationReadinessSummary:
    """Build deterministic orchestration readiness classification from eval summaries."""

    golden_case_ids = set(golden_summary["case_ids"])
    adversarial_case_ids = set(adversarial_summary["case_ids"])
    golden_cases_passed = golden_summary[
        "failed_cases"
    ] == 0 and _REQUIRED_GOLDEN_CASE_IDS.issubset(golden_case_ids)
    adversarial_cases_passed = adversarial_summary[
        "failed_cases"
    ] == 0 and _REQUIRED_ADVERSARIAL_CASE_IDS.issubset(adversarial_case_ids)
    coverage: OrchestrationCoverageSummary = {
        "execution_replay_safety": _all_cases_replayed(golden_summary, adversarial_summary),
        "audit_persistence_coverage": _audit_coverage_present(golden_summary, adversarial_summary),
        "synthesis_fallback_coverage": _case_passed(
            golden_summary, "golden_synthesis_disabled_fallback"
        )
        and _case_passed(adversarial_summary, "adversarial_blocked_synthesis_rollout"),
        "continuity_boundary_coverage": _case_passed(
            golden_summary, "golden_same_conversation_followup_reuse"
        )
        and _case_passed(adversarial_summary, "adversarial_cross_conversation_leakage")
        and _case_passed(adversarial_summary, "adversarial_cross_user_followup_leakage")
        and (
            _case_passed(adversarial_summary, "adversarial_blocked_continuity_rollout")
            or conversation_continuity_enabled
        ),
        "adversarial_fail_closed_coverage": adversarial_cases_passed,
        "supported_service_family_coverage": all(
            _case_passed(golden_summary, case_id)
            for case_id in (
                "golden_compute_single_step",
                "golden_grounded_knowledge_single_step",
                "golden_compute_plus_grounding_multi_step",
                "golden_forms_execution",
                "golden_reports_execution",
                "golden_document_extraction_execution",
            )
        ),
    }

    blocking_reasons: list[str] = []
    degraded_reasons: list[str] = []
    if not golden_cases_passed:
        blocking_reasons.append("golden_eval_failures")
    if not adversarial_cases_passed:
        blocking_reasons.append("adversarial_eval_failures")
    for coverage_key, covered in coverage.items():
        if not covered:
            blocking_reasons.append(coverage_key)

    if not blocking_reasons and (
        not response_synthesis_enabled or not conversation_continuity_enabled
    ):
        if not response_synthesis_enabled:
            degraded_reasons.append("response_synthesis_disabled")
        if not conversation_continuity_enabled:
            degraded_reasons.append("conversation_continuity_disabled")
        status = "degraded_safe"
    elif blocking_reasons:
        status = "not_ready"
    else:
        status = "ready"

    return {
        "status": status,
        "golden_cases_passed": golden_cases_passed,
        "adversarial_cases_passed": adversarial_cases_passed,
        "coverage": coverage,
        "required_case_ids_present": golden_cases_passed and adversarial_cases_passed,
        "blocking_reasons": blocking_reasons,
        "degraded_reasons": degraded_reasons,
    }


def _all_cases_replayed(
    golden_summary: OrchestrationEvalSummary,
    adversarial_summary: OrchestrationEvalSummary,
) -> bool:
    return all(
        result["replay_match"]
        for result in list(golden_summary["results"]) + list(adversarial_summary["results"])
    )


def _audit_coverage_present(
    golden_summary: OrchestrationEvalSummary,
    adversarial_summary: OrchestrationEvalSummary,
) -> bool:
    golden_has_audit = any(result["audit_event_count"] > 0 for result in golden_summary["results"])
    adversarial_has_audit = any(
        result["audit_event_count"] > 0 for result in adversarial_summary["results"]
    )
    return golden_has_audit and adversarial_has_audit


def _case_passed(summary: OrchestrationEvalSummary, case_id: str) -> bool:
    for result in summary["results"]:
        if result["case_id"] == case_id:
            return result["passed"]
    return False
