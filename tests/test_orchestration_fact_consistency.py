"""Focused coverage for governed cross-turn taxpayer fact consistency."""

from __future__ import annotations

from typing import cast

from services.orchestration.app.main import (
    _audit_fact_mismatch_summary,  # pyright: ignore[reportPrivateUsage]
)
from services.orchestration.app.main import (
    _select_prior_stated_facts_record,  # pyright: ignore[reportPrivateUsage]
)
from services.orchestration.app.fact_consistency import compare_stated_facts
from services.orchestration.app.llm_synthesis_context import build_governed_synthesis_context
from services.orchestration.app.llm_response_generator import (
    _build_structured_input,  # pyright: ignore[reportPrivateUsage]
)
from services.orchestration.app.conversation_state_store import ConversationStateRecord
from services.orchestration.app.prompt_semantic_extractor import ExtractedTaxpayerFacts
from services.orchestration.app.response_integrity_signals import FactMismatch
from services.orchestration.app.conversation_state_protection import unprotect_stated_facts
from services.orchestration.app.conversation_state_protection import (
    LocalAesGcmConversationStateProtector,
)


def test_compare_stated_facts_surfaces_conflicting_turnover_with_prior_execution() -> None:
    mismatches = compare_stated_facts(
        current=_facts(turnover_amount_kes=6000000.0),
        prior=_facts(turnover_amount_kes=4000000.0),
        prior_execution_id="execution-prior-001",
    )

    assert mismatches == [
        {
            "field": "turnover_amount_kes",
            "prior_value": 4000000.0,
            "prior_execution_id": "execution-prior-001",
            "current_value": 6000000.0,
        }
    ]
    assert _audit_fact_mismatch_summary(mismatches) == [
        {
            "field": "turnover_amount_kes",
            "prior_execution_id": "execution-prior-001",
        }
    ]


def test_newly_stated_fact_does_not_create_a_cross_turn_mismatch() -> None:
    assert (
        compare_stated_facts(
            current=_facts(turnover_amount_kes=4200000.0),
            prior=_facts(turnover_amount_kes=None),
            prior_execution_id="execution-prior-002",
        )
        == []
    )


def test_protected_prior_facts_round_trip_and_malformed_state_is_skipped() -> None:
    protector = LocalAesGcmConversationStateProtector(key=b"a" * 32)
    protected = protector.protect(_facts(turnover_amount_kes=4200000.0))

    assert unprotect_stated_facts(
        protected_stated_facts=protected,
        protector=protector,
    ) == _facts(turnover_amount_kes=4200000.0)
    assert (
        unprotect_stated_facts(
            protected_stated_facts={"algorithm": "AES-256-GCM", "nonce": "invalid"},
            protector=protector,
        )
        is None
    )


def test_anchor_case_provides_prior_turnover_to_synthesis_without_a_restatement() -> None:
    context = _context(
        prior_stated_facts=_facts(turnover_amount_kes=4200000.0),
        prior_execution_id="execution-prior-003",
        fact_mismatches=[],
    )

    structured_input = _build_structured_input(context)

    assert "=== TAXPAYER FACTS TO APPLY OR ADDRESS ===" in structured_input
    assert "turnover amount in KES as 4200000.0" in structured_input
    assert "execution-prior-003" in structured_input
    assert "Use it only if the current turn does not replace it." in structured_input


def test_mismatch_instruction_requires_explicit_resolution_without_silent_selection() -> None:
    mismatch = compare_stated_facts(
        current=_facts(turnover_amount_kes=6000000.0),
        prior=_facts(turnover_amount_kes=4000000.0),
        prior_execution_id="execution-prior-004",
    )
    context = _context(
        prior_stated_facts=_facts(turnover_amount_kes=4000000.0),
        prior_execution_id="execution-prior-004",
        fact_mismatches=mismatch,
    )

    structured_input = _build_structured_input(context)

    assert "Address this discrepancy explicitly" in structured_input
    assert "Do not silently use one value." in structured_input
    assert "as 4000000.0" in structured_input
    assert "now states 6000000.0" in structured_input


def test_latest_record_with_stated_facts_is_selected_over_newer_empty_state() -> None:
    selected = _select_prior_stated_facts_record(
        (
            _record("execution-empty", {}),
            _record("execution-facts", {"algorithm": "AES-256-GCM"}),
        )
    )

    assert selected is not None
    assert selected["execution_id"] == "execution-facts"


def _facts(*, turnover_amount_kes: float | None) -> ExtractedTaxpayerFacts:
    return {
        "income_amount_kes": None,
        "income_frequency": None,
        "turnover_amount_kes": turnover_amount_kes,
        "residency_status": None,
        "filing_status": None,
        "confidence_per_field": {"turnover_amount_kes": 0.98},
    }


def _context(
    *,
    prior_stated_facts: ExtractedTaxpayerFacts,
    prior_execution_id: str,
    fact_mismatches: list[FactMismatch],
):
    return build_governed_synthesis_context(
        prompt_text="Do I need to register for VAT?",
        tax_domain_hint="vat",
        intent_class="lookup_grounded_knowledge",
        plan={
            "plan_id": "plan-fact-consistency-001",
            "plan_status": "planned",
            "planning_mode": "single_step",
            "execution_ready": True,
            "steps": [],
        },
        mapped_result={"action_status": "resolved"},
        final_outcome={"message": "Knowledge lookup completed."},
        selected_route={
            "route_id": "knowledge_search_route_v1",
            "target_service": "knowledge",
            "target_operation": "search_knowledge",
        },
        adapter_response=None,
        step_results=None,
        step_summary=None,
        grounded_evidence=[
            {
                "source_id": "vat-source",
                "source_version_id": "vat-source-version",
                "anchor_id": "vat-anchor",
                "title": "VAT registration guidance",
                "url": "https://example.test/vat",
                "source_type": "tax_guidance",
                "tax_domain": "vat",
                "authority_level": "guidance",
                "effective_from": "2024-01-01",
                "effective_to": None,
                "tax_year": None,
                "publication_state": "published",
                "source_version_form": "point_in_time_consolidation",
                "grounding_status": "grounded",
                "content": "VAT registration applies above KES 5 million turnover.",
                "canonical_source_ref": "https://example.test/vat",
                "knowledge_route_mode": "search",
                "timeline_position": None,
                "canonical_claims": [
                    {
                        "entity_type": "regime",
                        "entity_label": "VAT registration",
                        "predicate": "threshold",
                        "polarity": "affirms",
                        "raw_value_text": "KES 5 million",
                        "normalized_value": {
                            "kind": "amount",
                            "raw_text": "KES 5 million",
                            "number_value": 5000000.0,
                            "currency_code": "KES",
                            "unit": "KES",
                            "frequency": "annual",
                            "scale": "million",
                        },
                        "taxpayer_category": "business",
                        "tax_domain": "vat",
                        "jurisdiction": "Kenya",
                        "jurisdiction_status": "verified",
                        "effective_from": "2024-01-01",
                        "effective_to": None,
                        "tax_year": 2024,
                        "period_type": "annual",
                        "current_effective": True,
                        "historical_effective": False,
                        "authority_level": "guidance",
                        "source_type": "tax_guidance",
                        "conditions": ["VAT registration threshold"],
                        "exceptions": [],
                        "claim_excerpt": "VAT registration applies above KES 5 million turnover.",
                        "claim_topic": "vat_threshold",
                        "extraction_confidence": 1.0,
                        "source_trust_status": "verified_official_source",
                        "provenance": {
                            "source_id": "vat-source",
                            "source_version_id": "vat-source-version",
                            "anchor_id": "vat-anchor",
                            "url": "https://example.test/vat",
                            "title": "VAT registration guidance",
                            "source_type": "tax_guidance",
                            "authority_level": "guidance",
                            "effective_from": "2024-01-01",
                            "effective_to": None,
                            "tax_year": 2024,
                            "source_trust_status": "verified_official_source",
                        },
                    }
                ],
            }
        ],
        explanation_items=None,
        citations=None,
        authority_summary=None,
        temporal_applicability=None,
        prior_stated_facts=prior_stated_facts,
        prior_execution_id=prior_execution_id,
        fact_mismatches=fact_mismatches,
    )


def _record(execution_id: str, stated_facts: dict[str, object]) -> ConversationStateRecord:
    return cast(
        ConversationStateRecord,
        {
            "execution_id": execution_id,
            "tenant_id": "tenant-001",
            "conversation_id": "conversation-001",
            "user_id": "user-001",
            "context_payload": {"stated_facts": stated_facts},
        },
    )
