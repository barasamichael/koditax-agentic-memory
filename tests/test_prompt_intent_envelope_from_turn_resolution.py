from __future__ import annotations

from services.orchestration.app.conversation_turn_resolution import ConversationTurnResolution
from services.orchestration.app.prompt_intent_envelope import build_prompt_intent_envelope_from_turn_resolution


def _turn_resolution(**changes: object) -> ConversationTurnResolution:
    data: dict[str, object] = {
        "schema_version": "1.0",
        "relationship": "standalone",
        "operation_mode": "informational",
        "raw_prompt": "What is VAT?",
        "contextualized_prompt": "What is VAT in Kenya?",
        "intent_class": "lookup_grounded_knowledge",
        "tax_domain_hint": "vat",
        "retrieval_tax_domain_filter": "vat",
        "jurisdiction_hint": "Kenya",
        "tax_year_hint": None,
        "answerability": "answerable",
        "clarification_reason_code": None,
        "clarification_question": None,
        "required_context_fields": [],
        "provided_context_fields": [],
        "missing_required_context_fields": [],
        "needs_knowledge_retrieval": True,
        "needs_computation": False,
        "needs_external_action": False,
        "needs_artifact_operation": False,
        "referenced_candidate_ids": [],
        "resolved_references": [],
        "retained_fields": [],
        "corrected_fields": [],
        "reuse_prior_semantic_facts": False,
        "reuse_prior_computation_result": False,
        "reuse_prior_evidence": False,
        "reuse_prior_artifact": False,
        "assumptions": [],
        "confidence": 0.95,
        "audit_summary": "standalone",
    }
    data.update(changes)
    return ConversationTurnResolution.model_validate(data)


def test_turn_resolution_adapter_maps_all_required_semantics() -> None:
    cases = [
        (
            _turn_resolution(),
            {
                "intent_class": "lookup_grounded_knowledge",
                "tax_domain_hint": "vat",
                "tax_year_hint": None,
                "operation_mode": "informational",
                "answerability": "answerable",
                "required_context_fields": (),
                "clarification_reason_code": None,
                "clarification_message": None,
                "assumptions": [],
                "retrieval_tax_domain_filter": "vat",
                "referenced_candidate_ids": (),
            },
        ),
        (
            _turn_resolution(
                operation_mode="informational",
                tax_domain_hint="general_tax",
                retrieval_tax_domain_filter=None,
                intent_class="lookup_grounded_knowledge",
                answerability="answerable_with_assumptions",
                assumptions=["Fish tax may mean levy, licence fee, VAT treatment or another local tax."],
                contextualized_prompt="What taxes, levies, licence fees or VAT treatment apply to fish tax in Kenya?",
                audit_summary="general tax",
            ),
            {
                "tax_domain_hint": "general_tax",
                "answerability": "answerable_with_assumptions",
                "assumptions": ["Fish tax may mean levy, licence fee, VAT treatment or another local tax."],
                "retrieval_tax_domain_filter": None,
            },
        ),
        (
            _turn_resolution(
                operation_mode="computation",
                intent_class="compute_income_tax",
                tax_year_hint=2026,
                answerability="clarification_required",
                clarification_reason_code="missing_income",
                clarification_question="Please provide taxable income.",
                required_context_fields=["taxable_income_or_income_components"],
                provided_context_fields=[],
                missing_required_context_fields=["taxable_income_or_income_components"],
                needs_knowledge_retrieval=False,
                needs_computation=True,
                contextualized_prompt="Calculate my tax for 2026.",
                audit_summary="computation clarification",
            ),
            {
                "operation_mode": "computation",
                "answerability": "clarification_required",
                "clarification_reason_code": "missing_income",
                "clarification_message": "Please provide taxable income.",
                "required_context_fields": ("taxable_income_or_income_components",),
                "provided_context_fields": (),
                "missing_required_context_fields": ("taxable_income_or_income_components",),
            },
        ),
        (
            _turn_resolution(
                relationship="meta_conversation",
                operation_mode="meta",
                intent_class="meta_conversation",
                tax_domain_hint="general_tax",
                needs_knowledge_retrieval=False,
                contextualized_prompt="Why did the previous request fail?",
                audit_summary="meta",
            ),
            {
                "relationship": "meta_conversation",
                "operation_mode": "meta",
                "answerability": "answerable",
                "needs_computation": False,
                "needs_external_action": False,
                "needs_artifact_operation": False,
            },
        ),
        (
            _turn_resolution(
                relationship="topic_shift",
                intent_class="lookup_grounded_knowledge",
                tax_domain_hint="general_tax",
                retrieval_tax_domain_filter=None,
                answerability="answerable_with_assumptions",
                assumptions=["The phrase may refer to a broad tax-related concept."],
                contextualized_prompt="What about fish tax?",
                audit_summary="broad lookup",
            ),
            {
                "tax_domain_hint": "general_tax",
                "retrieval_tax_domain_filter": None,
                "answerability": "answerable_with_assumptions",
            },
        ),
        (
            _turn_resolution(
                operation_mode="action",
                intent_class="request_action",
                needs_knowledge_retrieval=False,
                needs_external_action=True,
                contextualized_prompt="Please submit the form to KRA.",
                audit_summary="action",
            ),
            {
                "operation_mode": "action",
                "needs_external_action": True,
            },
        ),
        (
            _turn_resolution(
                operation_mode="artifact",
                intent_class="generate_report_artifact",
                needs_knowledge_retrieval=False,
                needs_artifact_operation=True,
                contextualized_prompt="Generate a PDF report.",
                audit_summary="artifact",
            ),
            {
                "operation_mode": "artifact",
                "needs_artifact_operation": True,
            },
        ),
    ]

    for turn_resolution, expected in cases:
        envelope = build_prompt_intent_envelope_from_turn_resolution(
            turn_resolution=turn_resolution,
            correlation_id="corr",
            trace_id="trace",
        )
        for key, value in expected.items():
            assert envelope[key] == value
        assert envelope["contextualized_prompt"] == turn_resolution.contextualized_prompt
        assert envelope["effective_prompt_text"] == turn_resolution.contextualized_prompt
