"""Deterministic eval harness coverage for governed orchestration runtime."""

from __future__ import annotations

from services.orchestration.app.orchestration_eval_harness import load_orchestration_eval_cases
from services.orchestration.app.orchestration_eval_harness import run_orchestration_eval_corpus


def test_golden_orchestration_eval_corpus_passes_for_supported_runtime_surface() -> None:
    summary = run_orchestration_eval_corpus(corpus="golden")

    assert summary["total_cases"] >= 8
    assert summary["failed_cases"] == 0
    assert summary["passed_cases"] == summary["total_cases"]


def test_adversarial_orchestration_eval_corpus_passes_fail_closed_expectations() -> None:
    summary = run_orchestration_eval_corpus(corpus="adversarial")

    assert summary["total_cases"] >= 9
    assert summary["failed_cases"] == 0
    assert summary["passed_cases"] == summary["total_cases"]


def test_orchestration_eval_harness_is_deterministic_across_repeated_runs() -> None:
    first = run_orchestration_eval_corpus(corpus="golden")
    second = run_orchestration_eval_corpus(corpus="golden")

    assert first == second


def test_orchestration_eval_corpora_cover_required_release_gate_case_ids() -> None:
    golden_ids = {case["case_id"] for case in load_orchestration_eval_cases("golden")}
    adversarial_ids = {case["case_id"] for case in load_orchestration_eval_cases("adversarial")}

    assert "golden_compute_single_step" in golden_ids
    assert "golden_grounded_knowledge_single_step" in golden_ids
    assert "golden_compute_plus_grounding_multi_step" in golden_ids
    assert "golden_forms_execution" in golden_ids
    assert "golden_reports_execution" in golden_ids
    assert "golden_document_extraction_execution" in golden_ids
    assert "golden_same_conversation_followup_reuse" in golden_ids
    assert "golden_synthesis_disabled_fallback" in golden_ids
    assert "adversarial_route_override_attempt" in adversarial_ids
    assert "adversarial_cross_conversation_leakage" in adversarial_ids
    assert "adversarial_cross_user_followup_leakage" in adversarial_ids
    assert "adversarial_citation_invention" in adversarial_ids
    assert "adversarial_blocked_continuity_rollout" in adversarial_ids
    assert "adversarial_blocked_synthesis_rollout" in adversarial_ids
    assert "adversarial_blocked_multi_step_execution" in adversarial_ids
    assert "adversarial_malformed_forms_payload" in adversarial_ids
