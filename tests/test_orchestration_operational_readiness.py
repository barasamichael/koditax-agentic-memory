"""Operational readiness classification checks for governed orchestration eval coverage."""

from __future__ import annotations

from copy import deepcopy

from services.orchestration.app.orchestration_eval_harness import run_orchestration_eval_corpus
from services.orchestration.app.orchestration_readiness_summary import (
    build_orchestration_readiness_summary,
)


def test_orchestration_readiness_reports_not_ready_when_full_eval_surface_does_not_pass() -> None:
    golden = run_orchestration_eval_corpus(corpus="golden")
    adversarial = run_orchestration_eval_corpus(corpus="adversarial")

    summary = build_orchestration_readiness_summary(
        golden_summary=golden,
        adversarial_summary=adversarial,
    )

    assert summary["status"] == "not_ready"
    assert summary["blocking_reasons"]
    assert summary["required_case_ids_present"] is False
    assert not all(summary["coverage"].values())


def test_orchestration_readiness_reports_not_ready_when_synthesis_is_disabled() -> None:
    golden = run_orchestration_eval_corpus(corpus="golden")
    adversarial = run_orchestration_eval_corpus(corpus="adversarial")

    summary = build_orchestration_readiness_summary(
        golden_summary=golden,
        adversarial_summary=adversarial,
        response_synthesis_enabled=False,
    )

    assert summary["status"] == "not_ready"
    assert summary["blocking_reasons"]
    assert summary["degraded_reasons"] == []


def test_orchestration_readiness_reports_not_ready_when_critical_guarantee_is_missing() -> None:
    golden = run_orchestration_eval_corpus(corpus="golden")
    adversarial = run_orchestration_eval_corpus(corpus="adversarial")
    broken = deepcopy(golden)
    broken["results"][0]["replay_match"] = False

    summary = build_orchestration_readiness_summary(
        golden_summary=broken,
        adversarial_summary=adversarial,
    )

    assert summary["status"] == "not_ready"
    assert "execution_replay_safety" in summary["blocking_reasons"]


def test_orchestration_readiness_reports_not_ready_when_required_case_ids_are_missing() -> None:
    golden = run_orchestration_eval_corpus(corpus="golden")
    adversarial = run_orchestration_eval_corpus(corpus="adversarial")
    broken = deepcopy(golden)
    broken["case_ids"] = broken["case_ids"][:-1]

    summary = build_orchestration_readiness_summary(
        golden_summary=broken,
        adversarial_summary=adversarial,
    )

    assert summary["status"] == "not_ready"
    assert summary["required_case_ids_present"] is False
    assert "golden_eval_failures" in summary["blocking_reasons"]
