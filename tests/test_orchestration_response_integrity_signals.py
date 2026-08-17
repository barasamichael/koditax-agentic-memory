"""Response integrity signal scaffold checks for orchestration synthesis."""

from __future__ import annotations

import pytest

from services.orchestration.app.main import (
    _compute_confidence_flag,  # pyright: ignore[reportPrivateUsage]
)
from services.orchestration.app.config import load_self_critique_config
from services.orchestration.app.audit_events import emit_income_tax_audit_event
from services.orchestration.app.audit_events import list_income_tax_audit_events
from services.orchestration.app.audit_events import InMemoryOrchestrationAuditEventStore
from services.orchestration.app.audit_events import set_default_orchestration_audit_event_store
from services.orchestration.app.audit_events import reset_default_orchestration_audit_event_store
from services.orchestration.app.llm_response_contract import UnifiedAnswerResponseModel
from services.orchestration.app.response_integrity_signals import ResponseIntegritySignals
from services.orchestration.app.synthesis_integrity_constants import MAX_VERIFICATION_RETRIES
from services.orchestration.app.synthesis_integrity_constants import MAX_SYNTHESIS_TOOL_ITERATIONS
from services.orchestration.app.synthesis_integrity_constants import FACT_EXTRACTION_MIN_CONFIDENCE


def test_unified_answer_response_includes_default_integrity_signals() -> None:
    response = UnifiedAnswerResponseModel(
        status="generated",
        answer_text="The governed answer is available.",
        answer_mode="compute_execution",
    )

    assert MAX_VERIFICATION_RETRIES == 1
    assert MAX_SYNTHESIS_TOOL_ITERATIONS == 3
    assert FACT_EXTRACTION_MIN_CONFIDENCE == 0.7
    assert response.model_dump()["integrity_signals"] == {
        "verification_is_verified": True,
        "verification_confidence": 1.0,
        "unsupported_claims": [],
        "contradictions_found": [],
        "grounding_contradictions": [],
        "unverified_or_contradicting_user_facts": [],
        "synthesis_tool_iterations_used": 0,
        "confidence_flag": "high",
    }


def test_response_synthesis_audit_payload_can_carry_self_critique_signals() -> None:
    set_default_orchestration_audit_event_store(InMemoryOrchestrationAuditEventStore())
    try:
        emit_income_tax_audit_event(
            event_type="response_synthesis_resolved",
            status="generated",
            correlation_id="corr-integrity-audit-001",
            trace_id="trace-integrity-audit-001",
            context={
                "tenant_id": "pilot_tenant_alpha",
                "user_id": "user_integrity_audit_001",
                "resource_id": "exec-integrity-audit-001",
                "decision_id": "decision-integrity-audit-001",
                "answer_mode": "grounded_knowledge",
                "unsupported_claims": ["Unsupported claim retained after retry."],
                "contradictions_found": ["Model-declared contradiction."],
            },
        )
        emit_income_tax_audit_event(
            event_type="response_synthesis_failed",
            status="failed",
            correlation_id="corr-integrity-audit-001",
            trace_id="trace-integrity-audit-001",
            context={
                "tenant_id": "pilot_tenant_alpha",
                "user_id": "user_integrity_audit_001",
                "resource_id": "exec-integrity-audit-002",
                "decision_id": "decision-integrity-audit-002",
                "reason_code": "response_synthesis_unavailable",
                "unsupported_claims": [],
                "contradictions_found": [],
            },
        )

        events = list_income_tax_audit_events(correlation_id="corr-integrity-audit-001")
    finally:
        reset_default_orchestration_audit_event_store()

    resolved = [event for event in events if event["event_type"] == "response_synthesis_resolved"]
    failed = [event for event in events if event["event_type"] == "response_synthesis_failed"]
    assert len(resolved) == 1
    assert len(failed) == 1
    assert resolved[0]["context"]["unsupported_claims"] == [
        "Unsupported claim retained after retry."
    ]
    assert resolved[0]["context"]["contradictions_found"] == ["Model-declared contradiction."]
    assert failed[0]["context"]["unsupported_claims"] == []
    assert failed[0]["context"]["contradictions_found"] == []


def test_self_critique_retry_default_remains_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "ORCHESTRATION_SELF_CRITIQUE_API_KEY",
        "OPENAI_API_KEY",
        "ORCHESTRATION_SELF_CRITIQUE_MODEL",
        "OPENAI_MODEL",
        "ORCHESTRATION_SELF_CRITIQUE_MAX_RETRIES",
        "ORCHESTRATION_SELF_CRITIQUE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    assert load_self_critique_config().max_retries == 0


def test_confidence_flag_prioritizes_integrity_failures_and_surfaces_model_signals() -> None:
    assert (
        _compute_confidence_flag(  # pyright: ignore[reportPrivateUsage]
            ResponseIntegritySignals(verification_is_verified=False)
        )
        == "low"
    )
    assert (
        _compute_confidence_flag(  # pyright: ignore[reportPrivateUsage]
            ResponseIntegritySignals(synthesis_tool_iterations_used=MAX_SYNTHESIS_TOOL_ITERATIONS)
        )
        == "low"
    )
    assert (
        _compute_confidence_flag(  # pyright: ignore[reportPrivateUsage]
            ResponseIntegritySignals(unverified_or_contradicting_user_facts=["turnover"])
        )
        == "low"
    )
    assert (
        _compute_confidence_flag(  # pyright: ignore[reportPrivateUsage]
            ResponseIntegritySignals(unsupported_claims=["An unsupported claim."])
        )
        == "medium"
    )
    assert (
        _compute_confidence_flag(  # pyright: ignore[reportPrivateUsage]
            ResponseIntegritySignals(contradictions_found=["A model-declared contradiction."])
        )
        == "medium"
    )
    assert (
        _compute_confidence_flag(  # pyright: ignore[reportPrivateUsage]
            ResponseIntegritySignals()
        )
        == "high"
    )
